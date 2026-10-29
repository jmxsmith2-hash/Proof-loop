"""Test-account credentials: typed by code, never shown to a model or written to logs."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from pydantic import SecretStr

PLACEHOLDER = re.compile(r"\{\{\s*cred:([A-Za-z0-9_]+)\s*\}\}")
MASK = "[secret]"


class CredentialVault:
    def __init__(self, values: Mapping[str, str | SecretStr] | None = None):
        self._values: dict[str, SecretStr] = {
            k: v if isinstance(v, SecretStr) else SecretStr(str(v)) for k, v in (values or {}).items()
        }

    def names(self) -> list[str]:
        return sorted(self._values)

    def is_placeholder(self, text: str) -> bool:
        return bool(PLACEHOLDER.search(text or ""))

    def resolve(self, text: str) -> str:
        """Replace {{cred:name}} with the secret. Only call right before typing."""
        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in self._values:
                raise KeyError(f"credential {name!r} is not configured for this target")
            return self._values[name].get_secret_value()
        return PLACEHOLDER.sub(_sub, text)

    def redact(self, text: str) -> str:
        for value in self._values.values():
            secret = value.get_secret_value()
            if secret and len(secret) >= 3:
                text = text.replace(secret, MASK)
        return text


class RedactingFilter(logging.Filter):
    """Logging filter that masks credential values in every record."""

    def __init__(self, vault: CredentialVault):
        super().__init__()
        self.vault = vault

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.vault.redact(str(record.msg))
        if record.args:
            record.args = tuple(self.vault.redact(str(a)) for a in record.args)
        return True
