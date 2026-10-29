"""What the ``proofloop check`` and ``proofloop web`` commands share: settings, the database, smart mode."""

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
    setup_logging(settings.log_level)
    return settings


def smart(settings: Settings, wanted: bool, batch: bool = False) -> Settings:
    if not wanted:
        return settings
    settings, note = settings.smart(batch=batch)
    typer.secho(note, fg="cyan" if "smart mode" in note else "yellow")
    return settings


def database(settings: Settings) -> Database:
    return Database(settings.db_path)
