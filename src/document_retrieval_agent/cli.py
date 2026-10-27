"""Read a local request, run an agent session, and display its reply and artifact paths."""

import argparse
import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path

from .agent import AgentSession
from .browser import UARBBrowser
from .config import ModelConfig
from .model import DEFAULT_MODEL, ChatModel
from .tools import Tools


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retrieve regulatory documents with a local agent."
    )
    parser.add_argument(
        "request", nargs="?", help="Natural-language request; omit to type it or pipe text on stdin"
    )
    parser.add_argument("--request-file", type=Path, help="Read a plain-text request from a file")
    parser.add_argument(
        "--no-input", action="store_true", help="Return clarification without prompting"
    )
    parser.add_argument(
        "--model",
        help=f"Model name (default: {DEFAULT_MODEL}; or set DOCUMENT_RETRIEVAL_AGENT_MODEL)",
    )
    parser.add_argument(
        "--api-base",
        help="OpenAI-compatible API base URL (or set DOCUMENT_RETRIEVAL_AGENT_API_BASE)",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser")
    parser.add_argument(
        "--output", type=Path, default=Path("runs"), help="Parent of run directories"
    )
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=600, help="Agent timeout in seconds")
    return parser


def read_request(args: argparse.Namespace) -> str:
    """Read exactly one input source: argument, file, pipe, or terminal prompt."""
    if args.request is not None and args.request_file:
        raise ValueError("Use either a request argument or --request-file")
    if args.request_file:
        request = args.request_file.read_text(encoding="utf-8")
    elif args.request is not None:
        request = args.request
    elif not sys.stdin.isatty():
        request = sys.stdin.read()
    elif not args.no_input:
        request = input("What documents would you like? ")
    else:
        request = ""
    if not request.strip():
        raise ValueError("Provide a request as text, through stdin, or with --request-file")
    return request.strip()


def read_follow_up() -> str | None:
    try:
        while True:
            answer = input("You: ").strip()
            if answer:
                return answer
    except EOFError:
        return None


def show_reply(result: dict, body: str) -> None:
    if result["status"] != "needs_input":
        print(f"Subject: {result['reply']['subject']}\n")
    print(body)


def show_summary(output: Path, result: dict) -> None:
    print(f"Status: {result['status']}\nResult: {output / 'result.json'}", file=sys.stderr)
    if result["archive"]:
        print(f"ZIP: {result['archive']}", file=sys.stderr)
    print(f"Draft: {output / 'reply.txt'}", file=sys.stderr)


def show_progress(tool_name: str) -> None:
    print(f"  {tool_name}", file=sys.stderr)


async def converse(session: AgentSession, request: str, *, interactive: bool) -> dict:
    while True:
        result, body = await session.respond(request)
        show_reply(result, body)
        if result["status"] != "needs_input" or not interactive:
            return result
        follow_up = read_follow_up()
        if follow_up is None:
            return result
        request = follow_up


async def run(request: str, config: ModelConfig, args: argparse.Namespace) -> int:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    output = args.output.resolve() / run_id
    output.mkdir(parents=True)
    print(f"Run directory: {output}", file=sys.stderr)

    async with UARBBrowser(headless=not args.headed) as browser:
        model = ChatModel(config.model, config.api_base, config.api_key)
        session = AgentSession(
            model,
            Tools(browser, output),
            max_calls=args.max_calls,
            timeout=args.timeout,
            progress=show_progress,
        )
        result = await converse(
            session,
            request,
            interactive=not args.no_input and sys.stdin.isatty(),
        )
    show_summary(output, result)
    return 0 if result["status"] in {"complete", "needs_input"} else 1


def parse_command_line() -> tuple[argparse.Namespace, ModelConfig, str]:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.max_calls < 1 or args.timeout <= 0:
            raise ValueError("--max-calls and --timeout must be positive")
        config = ModelConfig.from_environment(args.model, args.api_base)
        request = read_request(args)
    except ValueError as exc:
        parser.error(str(exc))
    return args, config, request


def main() -> int:
    try:
        args, config, request = parse_command_line()
        return asyncio.run(run(request, config, args))
    except EOFError:
        print("No request entered.", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
