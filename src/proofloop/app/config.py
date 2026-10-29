"""The window's ports and phone pairing (``phone.json`` in ProofLoop's folder, ``~/.proofloop``)."""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field

from proofloop.core.paths import home

DEFAULT_PORT = 8600
PHONE_PORT = 8601


@dataclass
class Settings:
    phone: bool = False  # phone access on the local network
    phone_port: int = PHONE_PORT
    phone_secret: str = field(default_factory=lambda: secrets.token_urlsafe(24))  # in the QR code's link
    phone_code: str = field(default_factory=lambda: f"{secrets.randbelow(10**6):06d}")  # to type instead

    @classmethod
    def load(cls) -> Settings:
        path = home() / "phone.json"
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
        path = home() / "phone.json"
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
