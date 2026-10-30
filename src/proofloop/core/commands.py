"""What the ``proofloop check`` commands share: settings and the database."""

from __future__ import annotations

from pathlib import Path

import typer

from proofloop.core.config import ConfigError, Settings, load_settings
from proofloop.core.db import Database
from proofloop.core.logging import setup_logging

ConfigOpt = typer.Option(None, "--config", "-c", help="Config folder (default: ~/.proofloop/config).")


def settings_or_exit(config: Path | None) -> Settings:
    try:
        settings = load_settings(config)
    except ConfigError as exc:
        typer.secho(f"Configuration error: {exc}", fg="red", err=True)
        raise typer.Exit(2) from None
    # The terminal shows warnings and worse; the background service logs everything to its file.
    setup_logging(settings.log_level if settings.log_level.upper() == "DEBUG" else "WARNING")
    return settings


def database(settings: Settings) -> Database:
    return Database(settings.db_path)
