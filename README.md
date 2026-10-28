# Document Retrieval Agent

An agent that retrieves public documents from the [Nova Scotia regulatory documents database](https://uarb.novascotia.ca/fmi/webd/UARB15). Give it a matter number and document category in plain English, through the CLI or email, and it returns up to ten files in a ZIP with a summary.

For example:

> Please get Key Documents for M12205.

Supported categories: Exhibits, Key Documents, Other Documents, Transcripts, and Recordings. Each request handles one matter and one category, taking the first ten documents in the website's displayed order.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
cp .env.example .env
```

Add your model key to `.env`:

```dotenv
GEMINI_API_KEY=your-key
```

The default model is `gemini-3.5-flash-lite`. To use another OpenAI-compatible provider with tool calling, set these instead:

```dotenv
DOCUMENT_RETRIEVAL_AGENT_API_BASE=https://your-provider.example/v1
DOCUMENT_RETRIEVAL_AGENT_MODEL=your-model
DOCUMENT_RETRIEVAL_AGENT_API_KEY=your-key
```

On Linux, missing browser libraries can be installed with `uv run playwright install-deps chromium`.

## Run locally

```bash
uv run document-retrieval-agent "Get Other Documents from M12205"
```

Run without an argument for an interactive conversation. If information is missing, the agent asks a follow-up question.

Useful options:

- `--headed`: watch the browser.
- `--request-file request.txt`: read a request from a text file.
- `--output ./runs`: choose where results are saved.
- `--timeout 600`: set the overall time limit in seconds.

Results are saved under `runs/`, including the ZIP, downloaded documents, a reply draft, and run logs.

## Email

Send a plain-text email to `agent@documents.julianbk.com` with the matter number and document category:

> Please get Other Documents for M12205.

The agent replies with a summary and ZIP attachment. If details are missing, send a new email containing both the matter number and category.

To run the email server yourself, add these to `.env` alongside the model key:

```dotenv
RESEND_API_KEY=your-resend-key
RESEND_WEBHOOK_SECRET=whsec_your-webhook-secret
AGENT_EMAIL=agent@documents.julianbk.com
```

The Resend API key needs access to received emails and permission to send replies. The webhook secret must match the specific Resend endpoint calling this server.

```bash
uv run uvicorn document_retrieval_agent.email.server:create_app \
  --factory --host 127.0.0.1 --port 8000 --workers 1
```

Configure Resend to send `email.received` events to the server's public `/webhooks/resend` endpoint. Use `/health` to check that the server is running.

## Overview

The agent understands your request and uses Playwright to find the matter and download its documents. It directs each retrieval step, can use browser actions to recover from errors, and packages the files into a ZIP with a summary. CLI and email requests use the same agent.
