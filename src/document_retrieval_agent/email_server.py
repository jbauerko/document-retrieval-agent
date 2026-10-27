"""Minimal Resend webhook: verify, deduplicate, run the agent, and email its result."""

import asyncio
import base64
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from svix.webhooks import Webhook, WebhookVerificationError

from .agent import AgentSession
from .browser import UARBBrowser
from .config import ModelConfig
from .model import ChatModel
from .tools import Tools

logger = logging.getLogger(__name__)
RESEND_API = "https://api.resend.com"
# A conservative encoded size cap, below Resend's 40 MB limit.
MAX_ATTACHMENT_BYTES = 25_000_000
NEW_REQUEST_INSTRUCTIONS = (
    "Please send a new email including the matter number and one document category: "
    "Exhibits, Key Documents, Other Documents, Transcripts, or Recordings. "
    "Each email is processed independently."
)


@dataclass(frozen=True)
class EmailConfig:
    resend_api_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    model: ModelConfig
    agent_email: str = "agent@documents.julianbk.com"
    state_path: Path = Path("data/email.sqlite3")
    output_dir: Path = Path("runs/email")

    @classmethod
    def from_environment(cls):
        model = ModelConfig.from_environment()  # Also loads .env.
        missing = [key for key in ("RESEND_API_KEY", "RESEND_WEBHOOK_SECRET") if not os.getenv(key)]
        if missing:
            raise ValueError(f"Set {', '.join(missing)} in .env before starting the email server")
        address = os.getenv("AGENT_EMAIL", "agent@documents.julianbk.com").strip().lower()
        if parseaddr(address)[1] != address or "@" not in address:
            raise ValueError("AGENT_EMAIL must be a plain email address")
        return cls(
            resend_api_key=os.environ["RESEND_API_KEY"],
            webhook_secret=os.environ["RESEND_WEBHOOK_SECRET"],
            model=model,
            agent_email=address,
            state_path=Path(os.getenv("EMAIL_STATE_PATH", "data/email.sqlite3")),
            output_dir=Path(os.getenv("EMAIL_OUTPUT_DIR", "runs/email")),
        )


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database:
        database.execute("""CREATE TABLE IF NOT EXISTS emails (
            email_id TEXT PRIMARY KEY, status TEXT NOT NULL, run_directory TEXT,
            reply_id TEXT, error TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        database.commit()


def claim_email(path: Path, email_id: str) -> bool:
    with closing(sqlite3.connect(path)) as database:
        cursor = database.execute(
            "INSERT OR IGNORE INTO emails (email_id, status) VALUES (?, 'processing')", (email_id,)
        )
        database.commit()
        return cursor.rowcount == 1


def record_status(config: EmailConfig, email_id: str, status: str, *, reply_id=None, error=None):
    with closing(sqlite3.connect(config.state_path)) as database:
        database.execute(
            """UPDATE emails SET status=?, run_directory=?, reply_id=?, error=?,
               updated_at=CURRENT_TIMESTAMP WHERE email_id=?""",
            (status, str((config.output_dir / email_id).resolve()), reply_id, error, email_id),
        )
        database.commit()


def addresses(value: str | list[str]) -> list[str]:
    values = [value] if isinstance(value, str) else value
    return [address.lower() for _, address in getaddresses(values)]


def is_automated(email: dict, own_address: str) -> bool:
    headers = {key.lower(): value for key, value in (email.get("headers") or {}).items()}
    sender = parseaddr(email.get("from", ""))[1].lower()
    return (
        sender == own_address.lower()
        or sender.split("@", 1)[0] in {"mailer-daemon", "postmaster"}
        or headers.get("auto-submitted", "no").strip().lower() != "no"
        or headers.get("precedence", "").strip().lower() in {"bulk", "list", "junk"}
    )


async def run_request(request: str, model_config: ModelConfig, directory: Path) -> dict:
    async with UARBBrowser() as browser:
        model = ChatModel(model_config.model, model_config.api_base, model_config.api_key)
        session = AgentSession(model, Tools(browser, directory))
        result, _ = await session.respond(request)
        return result


def prepare_reply(email: dict, result: dict, config: EmailConfig, directory: Path) -> dict:
    headers = {key.lower(): value for key, value in (email.get("headers") or {}).items()}
    reply_to = email.get("reply_to") or headers.get("reply-to") or email.get("from", "")
    recipients = addresses(reply_to)
    if not recipients or "@" not in recipients[0] or recipients[0] == config.agent_email.lower():
        raise ValueError("Received email has no valid external reply address")
    subject = email.get("subject") or "Document request"
    subject = subject.replace("\r", " ").replace("\n", " ")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    body = result["reply"]["body"]
    if result["status"] == "needs_input":
        body += f"\n\n{NEW_REQUEST_INSTRUCTIONS}"
    elif result["status"] in {"failed", "incomplete"}:
        body = f"Hi,\n\nI couldn't complete your document request. {NEW_REQUEST_INSTRUCTIONS}"

    attachments = []
    encoded_size = 0
    for filename in result["reply"].get("attachments", []):
        path = Path(filename).resolve()
        if not path.is_relative_to(directory.resolve()) or path.suffix.lower() != ".zip":
            raise ValueError("Reply attachments must be ZIP files from this email's run directory")
        encoded_size += 4 * ((path.stat().st_size + 2) // 3)
        if encoded_size > MAX_ATTACHMENT_BYTES:
            body = (
                "Hi,\n\nThe ZIP was created, but it is too large to attach to an email. "
                "Please send a new request for a different document category."
            )
            attachments = []
            break
        attachments.append(
            {"filename": path.name, "content": base64.b64encode(path.read_bytes()).decode()}
        )

    reply_headers = {"Auto-Submitted": "auto-replied"}
    message_id = email.get("message_id") or headers.get("message-id")
    if message_id:
        reply_headers["In-Reply-To"] = message_id
        reply_headers["References"] = (headers.get("references", "") + " " + message_id).strip()
    return {
        "from": config.agent_email,
        "to": [recipients[0]],
        "subject": subject,
        "text": body,
        "headers": reply_headers,
        "attachments": attachments,
    }


async def process_email(email_id: str, config: EmailConfig, runner, transport=None) -> None:
    """Process once; failures are recorded for manual reruns, never retried here."""
    try:
        directory = config.output_dir / email_id
        directory.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.resend_api_key}"},
            timeout=30,
            transport=transport,
        ) as client:
            response = await client.get(f"{RESEND_API}/emails/receiving/{email_id}")
            response.raise_for_status()
            email = response.json()
            if config.agent_email.lower() not in addresses(email.get("to", [])) or is_automated(
                email, config.agent_email
            ):
                record_status(config, email_id, "ignored")
                return

            text = (email.get("text") or "").strip()
            if text or (email.get("subject") and not email.get("html")):
                request = f"Subject: {email.get('subject', '')}\n\n{text}"
                result = await runner(request, config.model, directory)
            else:
                result = {
                    "status": "needs_input",
                    "reply": {
                        "body": "Hi, please send your request as plain text.",
                        "attachments": [],
                    },
                }
            payload = prepare_reply(email, result, config, directory)
            response = await client.post(
                f"{RESEND_API}/emails",
                json=payload,
                headers={"Idempotency-Key": f"document-retrieval-agent/{email_id}"},
            )
            response.raise_for_status()
            record_status(config, email_id, "sent", reply_id=response.json()["id"])
            logger.info("Email %s: reply accepted by Resend", email_id)
    except Exception as exc:
        error = (
            str(exc)
            .replace(config.resend_api_key, "[redacted]")
            .replace(config.model.api_key, "[redacted]")
        )
        record_status(config, email_id, "failed", error=error)
        logger.error("Email %s failed: %s", email_id, error)


def create_app(
    config: EmailConfig | None = None,
    *,
    runner: Callable[[str, ModelConfig, Path], Awaitable[dict]] = run_request,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = config or EmailConfig.from_environment()
    verifier = Webhook(config.webhook_secret)
    initialize_database(config.state_path)
    processing_lock = asyncio.Lock()
    app = FastAPI(title="Document Retrieval Agent Email")

    async def process_once(email_id: str):
        async with processing_lock:
            await process_email(email_id, config, runner, transport)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/webhooks/resend")
    async def receive_email(request: Request, background: BackgroundTasks):
        try:
            event = verifier.verify(await request.body(), dict(request.headers))
        except (WebhookVerificationError, ValueError) as exc:
            raise HTTPException(400, "Invalid webhook signature or payload") from exc
        if not isinstance(event, dict):
            raise HTTPException(400, "Invalid webhook event")
        if event.get("type") != "email.received":
            return {"status": "ignored"}
        data = event.get("data")
        if not isinstance(data, dict):
            raise HTTPException(400, "Missing email event data")
        try:
            email_id = str(UUID(str(data.get("email_id", ""))))
            recipients = addresses(data.get("to", []))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "Invalid email ID or recipients") from exc
        if config.agent_email.lower() not in recipients or is_automated(data, config.agent_email):
            return {"status": "ignored"}
        if not claim_email(config.state_path, email_id):
            return {"status": "duplicate"}
        background.add_task(process_once, email_id)
        return {"status": "accepted"}

    return app
