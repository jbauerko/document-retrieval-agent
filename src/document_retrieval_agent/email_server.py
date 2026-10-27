"""Compatibility entry point for existing uvicorn startup commands."""

from .email.server import create_app as create_app
