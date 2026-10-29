"""ProofLoop's own folder and settings (``~/.proofloop``; PROOFLOOP_HOME moves it)."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("PROOFLOOP_HOME") or Path.home() / ".proofloop")
DEFAULT_PORT = 8600
PHONE_PORT = 8601


def home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME


@dataclass
class Settings:
    phone: bool = False  # phone access on the local network
    phone_port: int = PHONE_PORT
    phone_secret: str = field(default_factory=lambda: secrets.token_urlsafe(24))  # in the QR code's link
    phone_code: str = field(default_factory=lambda: f"{secrets.randbelow(10**6):06d}")  # to type instead

    @classmethod
    def load(cls) -> Settings:
        path = home() / "settings.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        s = cls(**known)
        if known != asdict(s):
            s.save()  # new secrets are kept, so a paired phone stays paired
        return s

    def save(self) -> None:
        path = home() / "settings.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            path.chmod(0o600)  # holds the phone secret
        except OSError:
            pass

    def new_phone_secrets(self) -> None:
        """Forget every paired phone."""
        self.phone_secret = secrets.token_urlsafe(24)
        self.phone_code = f"{secrets.randbelow(10**6):06d}"
