"""Resolve model settings without exposing credentials in command-line arguments."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .agent.model import DEFAULT_API_BASE, DEFAULT_MODEL, MISSING_API_KEY_MESSAGE


@dataclass(frozen=True)
class ModelConfig:
    model: str
    api_base: str
    api_key: str = field(repr=False)

    @classmethod
    def from_environment(cls, model: str | None = None, api_base: str | None = None):
        load_dotenv(Path.cwd() / ".env")
        model = model or os.getenv("DOCUMENT_RETRIEVAL_AGENT_MODEL") or DEFAULT_MODEL
        api_base = api_base or os.getenv("DOCUMENT_RETRIEVAL_AGENT_API_BASE") or DEFAULT_API_BASE
        api_key = os.getenv("DOCUMENT_RETRIEVAL_AGENT_API_KEY")
        if not api_key and api_base.rstrip("/") == DEFAULT_API_BASE:
            api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(MISSING_API_KEY_MESSAGE)
        return cls(model=model, api_base=api_base, api_key=api_key)
