# Document Retrieval Agent

A document retrieval agent that interprets a request, retrieves up to ten documents from a Nova Scotia regulatory matter, and creates a ZIP and reply. Run it locally through the CLI, or connect Resend to the email webhook server below.

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync
uv run playwright install chromium
cp .env.example .env
```

On Linux, if Chromium reports missing system libraries, install them with `uv run playwright install-deps chromium` (requires system package installation permissions).

Add a [Google AI Studio API key](https://aistudio.google.com/apikey) to `.env`:

```dotenv
GEMINI_API_KEY=<your-key>
```

That's the only required setting for the CLI. The default model is `gemini-3.5-flash-lite`, accessed through [Google's OpenAI-compatible endpoint](https://ai.google.dev/gemini-api/docs/openai). The project uses a small async HTTP client, with no model SDK or gateway to install.

To switch to another **OpenAI-compatible, tool-calling endpoint**, set all three settings:

```dotenv
DOCUMENT_RETRIEVAL_AGENT_API_BASE=https://your-provider.example/v1
DOCUMENT_RETRIEVAL_AGENT_MODEL=your-model-name
DOCUMENT_RETRIEVAL_AGENT_API_KEY=your-provider-key
```

Use the model name that endpoint expects, without a LiteLLM provider prefix. For a local endpoint that does not require authentication, use any nonempty placeholder key. A Gemini key is only used with the default Gemini endpoint.

## Run

```bash
uv run document-retrieval-agent "Get Other Documents from M12205"
```

Requests can be conversational, for example `"Hi Agent, could you grab the key docs for m 12205? Thanks!"`. Matter numbers and common document-category abbreviations are interpreted by the model.

To start an interactive request, run without an argument:

```bash
uv run document-retrieval-agent
```

If a request is missing a matter number or document category, the agent asks a follow-up question in an interactive terminal. Answer in the same session; the original request is retained. For example:

```text
What documents would you like? Get the documents for M12205
Which document category would you like: Exhibits, Key Documents, Other Documents, Transcripts, or Recordings?
You: Other docs, please
```

You can also use a plain-text file or pipe a request, which will be useful for later email intake:

```bash
uv run document-retrieval-agent --request-file request.txt
printf '%s\n' 'Please get Other Documents for M12205' | uv run document-retrieval-agent
```

Piped runs never wait for clarification input. Use `--no-input` to get the same one-shot behavior in an interactive terminal. In either case, an ambiguous request returns `status: needs_input` and a clarification reply without starting the browser.

Use `--headed` to watch the browser, `--model model-name` to override the configured model, or `--output /path/to/runs` to choose the output parent. `--timeout` defaults to 600 seconds and `--max-calls` to 100.

The agent supports Exhibits, Key Documents, Other Documents, Transcripts, and Recordings. Each run handles one matter and one category.

Each run creates its own directory:

```text
runs/<timestamp-id>/
  downloads/          # Successfully downloaded files
  M12205_other_documents.zip
  result.json        # Metadata, counts, downloads, failures, status, usage, structured reply
  reply.txt          # Plain-text email draft with subject and body; nothing is sent
  events.jsonl       # Tool calls and results for debugging
```

Exit code `0` means complete or clarification needed; `1` means incomplete, partial, or failed; `130` means interrupted. Inspect `result.json` for the exact outcome. A category with zero documents produces a valid empty ZIP. Failed retrievals do not produce a misleading empty ZIP. A partially successful run can produce a ZIP of its successful downloads and reports the failures.

The `reply` object in `result.json` is also consumed by the email server:

```json
{
  "subject": "M12205 — Other Documents",
  "body": "Hi, ... your ZIP is ready ...",
  "attachments": ["/absolute/path/to/M12205_other_documents.zip"]
}
```

The model is instructed to write a short plain-text email body including the matter's regulatory type/category, dates, all five document counts, and download outcome. The subject and attachment list are built from run state. Clarification and failed runs have no ZIP attachment; partial runs attach only successfully packaged downloads. The complete original request remains in `result.json` after follow-up answers.

## How it works

```text
src/document_retrieval_agent/
  agent/
    session.py       # Conversation, model turns, tool execution, budgets
    model.py         # Provider-neutral HTTP model client
    prompts.py       # Agent instructions
  retrieval/
    browser.py       # Playwright tasks and recovery actions
    observations.py  # Page observation logic used by the browser
    tools.py         # Retrieval state, task tools, download/ZIP checks
    schemas.py       # Tool definitions exposed to the model
    documents.py     # Categories, filenames, and observed-data validation
  email/
    server.py        # Resend webhook, duplicate tracking, and replies
  cli.py             # Local request input and terminal output
  config.py          # Model settings and environment loading
  output.py          # Result, event trace, and reply files
  email_server.py    # Compatibility entry point for existing server commands
```

`AgentSession` owns the bounded conversation/tool loop. Both the CLI and email server
compose it with the same model client and retrieval tools. Browser observations live
inside the Python package; there is no separate JavaScript asset to load or package.

The model normally calls `search_matter(matter_number, document_type)`, inspects the returned metadata/counts, and calls `open_category(category)` to read the first ten document IDs/titles. It then chooses each `download_document(document_id)` individually, evaluates successes/failures, and calls `create_archive()` after attempting the listing. Playwright handles search fields, tab switches, download dialogs and saving files inside those task tools. Successful tasks return compact results, reducing model turns and repeated page text.

The model still interprets requests, asks clarifications, directs the sequence, chooses individual downloads and retry/recovery actions, and writes the reply. On unexpected states, task errors include a page observation. The model can use `observe_page`, `click`, `fill`, `press_key`, `scroll`, and `download` to inspect and recover. `open_site`, `record_matter`, and `record_documents` also remain available if the automated search or extraction cannot handle a changed layout. Element references expire after observations/actions; task success retires older observations too.

Code validates matter identity, category counts, metadata and row IDs/titles against the page, enforces one matter/category and a ten-file limit, verifies files, and checks the ZIP. Both CLI and email use the same loop. Task automation uses known FileMaker selectors; fallback actions use generic DOM observations and temporary refs. Navigation is restricted to the regulatory website. Full observations remain in `events.jsonl`, while only the latest observation stays in model context. HTTP 429 retries wait up to a minute within the run timeout. Use `--headed` to watch navigation.

## Email webhook server

The configured address is `agent@documents.julianbk.com`. Resend must have sending and receiving verified for `documents.julianbk.com`.

Add these settings to your existing `.env` alongside the model key:

```dotenv
RESEND_API_KEY=your-resend-key
RESEND_WEBHOOK_SECRET=whsec_your-webhook-signing-secret
AGENT_EMAIL=agent@documents.julianbk.com
```

The Resend key needs permission to retrieve received emails and send replies. The webhook signing secret is a separate value from the API key; copy it from your webhook's details in Resend.

For a local test:

1. Install dependencies with `uv sync` and Chromium with `uv run playwright install chromium`.
2. Open an HTTPS tunnel to local port 8000, for example `ngrok http 8000` if you have [ngrok configured](https://ngrok.com/docs/getting-started/).
3. In Resend's Webhooks dashboard, add `https://YOUR-TUNNEL-URL/webhooks/resend` and select **email.received**.
4. Copy the webhook signing secret into `.env` and start the server:

```bash
uv run uvicorn document_retrieval_agent.email.server:create_app \
  --factory --host 127.0.0.1 --port 8000 --workers 1
```

The previous `document_retrieval_agent.email_server:create_app` startup path also works.

5. Check `http://127.0.0.1:8000/health`, then send a real plain-text email to `agent@documents.julianbk.com`, such as "Get Other Documents from M12205".

The server verifies signatures on the raw request body and adds the received email ID to an in-memory set before returning a successful acknowledgment. Repeated events are skipped even while the original request is still processing. A background task fetches the message text, runs the existing agent, and sends the result from the configured address. The response preserves the original email subject and threading headers. Outgoing recipients come from the received message's reply address, never from the model.

Only messages addressed to `AGENT_EMAIL` are processed. Messages from the agent itself, common automated replies, bulk/list messages, and bounces are ignored. Each task gets a fresh browser session and a directory under `runs/email/<received-email-id>/`. One background task runs at a time; keep one server worker and avoid auto-reload during a request.

Duplicate tracking lasts for the lifetime of one application process and resets on restart or redeploy. An older event delivered after restart can run the agent again. Sends still include a stable idempotency key; Resend's 24-hour idempotency window helps prevent duplicate replies but does not prevent repeating retrieval work. Run a single server worker and replica so requests share the same set.

This deliberately small MVP has no database, persistent job queue, automatic delivery retries, or restart recovery. If the process stops during a task or a send fails, send a new complete request email to rerun it. Failed requests also remain in the set until restart. Failures are written to the server log; outgoing send and delivery status can be inspected in Resend.

Every email is an independent request. Missing details receive a clarification asking for a **new email with both the matter number and category**. HTML-only messages receive instructions to send plain text. ZIPs larger than the application's 25 MB encoded attachment cap receive an explanatory reply without an attachment. Other incoming attachments are not processed.

Optional storage overrides:

```dotenv
EMAIL_OUTPUT_DIR=./runs/email
```

A persistent volume is not required. Downloads and run output can use the deployment's ephemeral filesystem; attach persistent storage only if you want to retain those files across redeployments. Expose the server through HTTPS; a local tunnel URL must be updated in Resend if it changes.

## Checks

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Most tests use simulated model responses and browser results; browser action tests use local HTML in Chromium. No API key is required. Tests cover ZIP contents, the download limit, duplicate calls, partial failures, empty categories, unknown tools, clarification, budgets, and timeouts. A CLI integration test exercises request parsing, model HTTP messages, the agent loop, ZIP creation, and the draft together.

Opt-in checks use the real website and download public files, without a model or API key:

```bash
DOCUMENT_RETRIEVAL_AGENT_LIVE_TEST=1 uv run pytest tests/test_live.py -v -s
```

These check the first ten M12205 Other Documents, an empty category, and a nonexistent matter. Real-site checks depend on site availability and its current data.

Once the API key is configured, run the real model and real website together:

```bash
DOCUMENT_RETRIEVAL_AGENT_E2E_TEST=1 uv run pytest tests/test_e2e.py -v -s
```

This runs a conversational CLI request for M12205 Key Documents, verifies the ZIP against the manifest, and checks the draft's subject, attachment list, category counts, regulatory type/category, and tool trace. It makes model API calls using your configured provider and its normal billing/rate limits. If successful, pytest prints the run directory so you can inspect the artifacts.

## MVP limits

- Persistent hosting is not configured by the repository; the email server can be tested through a local HTTPS tunnel.
- The model must support function/tool calling. A real model run requires configuring a provider or a local model; browser checks alone do not validate model quality.
- Browser observations are DOM/text based, without screenshots or coordinate clicking. The model handles navigation changes, but validation still expects category count labels and document rows. Major site changes may need updates.
- Each document record is treated as one download; records with multiple attachments need additional handling.
- Draft wording is generated by the model. `result.json` is the source of truth for observed counts and downloaded files. Dates retain their website labels and formatting; unavailable metadata is not invented.
- API errors, tool results, and the request are stored locally. Keep `.env` and run artifacts out of version control.
