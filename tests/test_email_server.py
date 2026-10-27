import base64
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from svix.webhooks import Webhook

from document_retrieval_agent.config import ModelConfig
from document_retrieval_agent.email_server import EmailConfig, create_app, prepare_reply

SECRET = "whsec_" + base64.b64encode(b"test-webhook-signing-secret").decode()
ADDRESS = "agent@documents.julianbk.com"


def signed_headers(payload, timestamp=None):
    timestamp = timestamp or datetime.now(UTC)
    message_id = "msg_test"
    return {
        "svix-id": message_id,
        "svix-timestamp": str(int(timestamp.timestamp())),
        "svix-signature": Webhook(SECRET).sign(message_id, timestamp, payload),
        "content-type": "application/json",
    }


def configuration(tmp_path):
    return EmailConfig(
        resend_api_key="test-resend-key",
        webhook_secret=SECRET,
        model=ModelConfig("test-model", "https://model.test/v1", "test-model-key"),
        state_path=tmp_path / "email.sqlite3",
        output_dir=tmp_path / "runs",
    )


@pytest.fixture
async def mail(tmp_path):
    config = configuration(tmp_path)
    state = SimpleNamespace(
        config=config,
        received=[],
        sent=[],
        requests=[],
        retrieval_status="complete",
        get_status=200,
        send_status=200,
        incoming={
            "from": "User <user@example.com>",
            "to": [ADDRESS],
            "subject": "Documents please",
            "text": "Hi, get Other Documents for M12205.",
            "message_id": "<original@example.com>",
            "headers": {"References": "<previous@example.com>"},
        },
    )

    async def runner(request, model_config, directory):
        state.requests.append(request)
        assert model_config == config.model
        path = directory / "documents.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("document.pdf", b"%PDF-test-content")
        return {
            "status": state.retrieval_status,
            "reply": {
                "body": "Hi, your ZIP for M12205 is ready.",
                "attachments": [str(path)] if state.retrieval_status == "complete" else [],
            },
        }

    def respond(request):
        assert request.headers["Authorization"] == "Bearer test-resend-key"
        if request.method == "GET":
            state.received.append(str(request.url))
            return httpx.Response(state.get_status, json=state.incoming)
        assert request.method == "POST" and str(request.url) == "https://api.resend.com/emails"
        state.sent.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(state.send_status, json={"id": "outbound-test-id"})

    state.app = create_app(config, runner=runner, transport=httpx.MockTransport(respond))
    state.event = {
        "type": "email.received",
        "data": {
            "email_id": str(uuid4()),
            "from": "user@example.com",
            "to": [ADDRESS],
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url="http://test"
    ) as client:
        state.client = client

        async def deliver(event=None, headers=None, payload=None):
            payload = payload or json.dumps(event or state.event)
            return await client.post(
                "/webhooks/resend", content=payload, headers=headers or signed_headers(payload)
            )

        state.deliver = deliver
        yield state


def stored_status(mail):
    with closing(sqlite3.connect(mail.config.state_path)) as database:
        return database.execute("SELECT status, reply_id, error FROM emails").fetchone()


async def test_verified_email_runs_agent_and_sends_threaded_zip(mail):
    response = await mail.deliver()
    assert response.json() == {"status": "accepted"}
    assert mail.requests == ["Subject: Documents please\n\nHi, get Other Documents for M12205."]
    headers, sent = mail.sent[0]
    assert (
        headers["idempotency-key"] == f"document-retrieval-agent/{mail.event['data']['email_id']}"
    )
    assert sent["from"] == ADDRESS
    assert sent["to"] == ["user@example.com"]
    assert sent["subject"] == "Re: Documents please"
    assert sent["headers"]["In-Reply-To"] == "<original@example.com>"
    assert sent["headers"]["References"] == "<previous@example.com> <original@example.com>"
    assert sent["headers"]["Auto-Submitted"] == "auto-replied"
    path = mail.config.output_dir / mail.event["data"]["email_id"] / "documents.zip"
    assert base64.b64decode(sent["attachments"][0]["content"]) == path.read_bytes()
    assert stored_status(mail) == ("sent", "outbound-test-id", None)


async def test_duplicate_delivery_does_not_run_or_send_again(mail):
    assert (await mail.deliver()).json()["status"] == "accepted"
    assert (await mail.deliver()).json()["status"] == "duplicate"
    assert len(mail.requests) == len(mail.sent) == 1
    # A fresh application instance still recognizes the stored message ID.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(mail.config)), base_url="http://test"
    ) as restarted:
        payload = json.dumps(mail.event)
        response = await restarted.post(
            "/webhooks/resend", content=payload, headers=signed_headers(payload)
        )
    assert response.json() == {"status": "duplicate"}


async def test_tampered_payload_is_rejected_before_processing(mail):
    payload = json.dumps(mail.event)
    response = await mail.deliver(payload=payload + " ", headers=signed_headers(payload))
    assert response.status_code == 400
    assert mail.received == mail.requests == mail.sent == []


async def test_expired_signature_is_rejected(mail):
    payload = json.dumps(mail.event)
    headers = signed_headers(payload, datetime.now(UTC) - timedelta(minutes=10))
    assert (await mail.deliver(payload=payload, headers=headers)).status_code == 400
    assert mail.received == []


@pytest.mark.parametrize("missing", ["svix-id", "svix-timestamp", "svix-signature"])
async def test_missing_signature_headers_are_rejected(mail, missing):
    payload = json.dumps(mail.event)
    headers = signed_headers(payload)
    del headers[missing]
    assert (await mail.deliver(payload=payload, headers=headers)).status_code == 400
    assert mail.received == []


@pytest.mark.parametrize("change", ["other_event", "other_address", "self", "bounce"])
async def test_unrelated_messages_are_ignored(mail, change):
    if change == "other_event":
        mail.event["type"] = "email.delivered"
    elif change == "other_address":
        mail.event["data"]["to"] = ["other@documents.julianbk.com"]
    elif change == "self":
        mail.event["data"]["from"] = ADDRESS
    else:
        mail.event["data"]["from"] = "MAILER-DAEMON@example.com"
    assert (await mail.deliver()).json() == {"status": "ignored"}
    assert mail.received == mail.requests == mail.sent == []


async def test_auto_reply_is_filtered_after_fetching_headers(mail):
    mail.incoming["headers"]["Auto-Submitted"] = "auto-replied"
    assert (await mail.deliver()).status_code == 200
    assert len(mail.received) == 1
    assert mail.requests == mail.sent == []
    assert stored_status(mail)[0] == "ignored"


async def test_clarification_requests_a_new_complete_email(mail):
    mail.retrieval_status = "needs_input"
    (await mail.deliver())
    sent = mail.sent[0][1]
    assert "new email including the matter number" in sent["text"]
    assert "Each email is processed independently" in sent["text"]
    assert sent["attachments"] == []


async def test_html_only_email_gets_plain_text_instructions_without_running_agent(mail):
    mail.incoming["text"] = None
    mail.incoming["html"] = "<p>Get M12205 exhibits</p>"
    (await mail.deliver())
    assert mail.requests == []
    assert "plain text" in mail.sent[0][1]["text"]
    assert mail.sent[0][1]["attachments"] == []


async def test_request_in_subject_with_empty_body_is_processed(mail):
    mail.incoming["subject"] = "Other Documents for M12205"
    mail.incoming["text"] = ""
    await mail.deliver()
    assert mail.requests == ["Subject: Other Documents for M12205\n\n"]
    assert len(mail.sent[0][1]["attachments"]) == 1


async def test_reply_to_header_selects_recipient_and_preserves_re_subject(mail):
    mail.incoming["reply_to"] = ["Reply User <reply@example.com>"]
    mail.incoming["subject"] = "Re: Existing thread"
    (await mail.deliver())
    assert mail.sent[0][1]["to"] == ["reply@example.com"]
    assert mail.sent[0][1]["subject"] == "Re: Existing thread"


async def test_delivery_failure_is_recorded_without_automatic_retry(mail):
    mail.send_status = 503
    (await mail.deliver())
    assert stored_status(mail)[0] == "failed"
    assert len(mail.sent) == 1
    assert (await mail.deliver()).json() == {"status": "duplicate"}
    assert len(mail.sent) == 1


async def test_oversized_zip_sends_an_explanation(mail, monkeypatch):
    monkeypatch.setattr("document_retrieval_agent.email_server.MAX_ATTACHMENT_BYTES", 4)
    (await mail.deliver())
    assert mail.sent[0][1]["attachments"] == []
    assert "too large to attach" in mail.sent[0][1]["text"]


async def test_failed_retrieval_does_not_email_internal_error_details(mail):
    mail.retrieval_status = "failed"
    (await mail.deliver())
    sent = mail.sent[0][1]
    assert "couldn't complete" in sent["text"]
    assert "ZIP" not in sent["text"]
    assert sent["attachments"] == []


async def test_health_endpoint_does_not_require_webhook_headers(mail):
    assert (await mail.client.get("/health")).json() == {"status": "ok"}


async def test_fetch_failure_is_recorded_without_running_agent(mail):
    mail.get_status = 401
    await mail.deliver()
    assert stored_status(mail)[0] == "failed"
    assert mail.requests == mail.sent == []


async def test_invalid_received_email_id_is_rejected(mail):
    mail.event["data"]["email_id"] = "../../private-file"
    assert (await mail.deliver()).status_code == 400
    assert mail.received == []


def test_attachment_outside_run_directory_is_rejected(tmp_path):
    config = configuration(tmp_path)
    directory = tmp_path / "run"
    directory.mkdir()
    other = tmp_path / "private.zip"
    other.write_bytes(b"private data")
    result = {"status": "complete", "reply": {"body": "Ready", "attachments": [str(other)]}}
    with pytest.raises(ValueError, match="from this email's run directory"):
        prepare_reply({"from": "user@example.com"}, result, config, directory)


async def test_webhook_acknowledgment_is_sent_before_agent_starts(tmp_path):
    config = configuration(tmp_path)
    acknowledged = False
    agent_ran = False
    email_id = str(uuid4())
    event = {
        "type": "email.received",
        "data": {"email_id": email_id, "from": "user@example.com", "to": [ADDRESS]},
    }
    payload = json.dumps(event)

    async def runner(request, model_config, directory):
        nonlocal agent_ran
        assert acknowledged, "The webhook response must finish before browser/model work"
        agent_ran = True
        return {"status": "needs_input", "reply": {"body": "Which category?", "attachments": []}}

    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "from": "user@example.com",
                    "to": [ADDRESS],
                    "text": "Get M12205",
                    "subject": "Documents",
                    "message_id": "<original@example.com>",
                },
            )
        return httpx.Response(200, json={"id": "reply-id"})

    app = create_app(config, runner=runner, transport=httpx.MockTransport(respond))

    async def receive():
        return {"type": "http.request", "body": payload.encode(), "more_body": False}

    async def send(message):
        nonlocal acknowledged
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            acknowledged = True

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/webhooks/resend",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (key.encode(), value.encode()) for key, value in signed_headers(payload).items()
        ],
        "server": ("test", 80),
        "client": ("127.0.0.1", 1000),
    }
    await app(scope, receive, send)
    assert agent_ran


async def test_default_runner_uses_existing_agent_and_saves_its_result(tmp_path, monkeypatch):
    from document_retrieval_agent import email_server

    config = configuration(tmp_path)
    browser_closed = False

    class Browser:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            nonlocal browser_closed
            browser_closed = True

    class Model:
        async def complete(self, messages, tools):
            assert messages[-1]["content"] == "Get documents for M12205"
            return {"role": "assistant", "content": "Which document category would you like?"}

    def model_factory(model, base, key):
        assert (model, base, key) == (
            config.model.model,
            config.model.api_base,
            config.model.api_key,
        )
        return Model()

    monkeypatch.setattr(email_server, "UARBBrowser", Browser)
    monkeypatch.setattr(email_server, "ChatModel", model_factory)
    result = await email_server.run_request(
        "Get documents for M12205", config.model, tmp_path / "run"
    )
    assert result["status"] == "needs_input"
    assert result["reply"]["attachments"] == []
    assert (tmp_path / "run" / "result.json").is_file()
    assert browser_closed
