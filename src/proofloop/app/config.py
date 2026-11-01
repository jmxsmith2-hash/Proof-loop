"""The window's ports and phone pairing (``phone.json`` in ProofLoop's folder, ``~/.proofloop``).

A phone pairs as one of two roles, each with its own secret (in the QR code's link) and six-digit code:
*operator* (``phone_secret`` and ``phone_code``, the names phones paired under before roles existed, so
those phones stay paired, as operators) and *watch only* (``phone_watch_secret``, ``phone_watch_code``).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field

from proofloop.core.paths import home

DEFAULT_PORT = 8600
PHONE_PORT = 8601


def _new_secret() -> str:
    return secrets.token_urlsafe(24)


def _new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


@dataclass
class Settings:
    phone: bool = False  # phone access on the local network
    phone_port: int = PHONE_PORT
    phone_secret: str = field(default_factory=_new_secret)  # operator: in the QR code's link
    phone_code: str = field(default_factory=_new_code)  # operator: to type instead
    phone_watch_secret: str = field(default_factory=_new_secret)  # watch only: in the QR code's link
    phone_watch_code: str = field(default_factory=_new_code)  # watch only: to type instead

    @classmethod
    def load(cls) -> Settings:
        path = home() / "phone.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        s = cls(**known)
        s._keep_roles_apart()
        if known != asdict(s):
            s.save()  # new secrets are kept, so a paired phone stays paired
        return s

    def save(self) -> None:
        path = home() / "phone.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            path.chmod(0o600)  # holds the phone secrets
        except OSError:
            pass

    def _keep_roles_apart(self) -> None:
        """The two roles never share a secret or a code, so a cookie or a typed code names exactly one."""
        while self.phone_watch_secret == self.phone_secret:
            self.phone_watch_secret = _new_secret()
        while self.phone_watch_code == self.phone_code:
            self.phone_watch_code = _new_code()

    def new_phone_secrets(self) -> None:
        """Forget every paired phone, watch-only and operator alike."""
        self.phone_secret, self.phone_code = _new_secret(), _new_code()
        self.phone_watch_secret, self.phone_watch_code = _new_secret(), _new_code()
        self._keep_roles_apart()
