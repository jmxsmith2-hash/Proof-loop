"""Signed job manifests.

Automation is armed by the JOB_START marker inside the G-code, never by the file
name, so renaming a file changes nothing. The manifest records how a file was
processed and a hash of the G-code it describes. It is stored twice: as
``Metadata/clearloop.json`` in the 3MF and as a comment line in the G-code
itself (which survives tools that drop unknown 3MF entries). With a farm secret
configured the manifest is HMAC-signed, so an edited or re-sliced file is caught
before it runs with a stale plan.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

LINE_PREFIX = "; CLEARLOOP_MANIFEST "
ENTRY_NAME = "Metadata/clearloop.json"


def _canonical(data: dict) -> bytes:
    return json.dumps(
        {k: v for k, v in data.items() if k != "sig"}, sort_keys=True, separators=(",", ":")
    ).encode()


def gcode_digest(gcode: str) -> str:
    body = "".join(line for line in gcode.splitlines(keepends=True) if not line.startswith(LINE_PREFIX))
    return hashlib.sha256(body.encode()).hexdigest()


def sign(data: dict, secret: str | None) -> dict:
    out = dict(data)
    out["sig"] = hmac.new(secret.encode(), _canonical(out), hashlib.sha256).hexdigest() if secret else None
    return out


def embed(gcode: str, data: dict) -> str:
    """Put the manifest line at the top of the G-code."""
    payload = base64.b64encode(json.dumps(data, sort_keys=True).encode()).decode()
    stripped = "".join(line for line in gcode.splitlines(keepends=True) if not line.startswith(LINE_PREFIX))
    return f"{LINE_PREFIX}{payload}\n{stripped}"


def extract(gcode: str) -> dict | None:
    head = gcode[:65536]
    for line in head.splitlines():
        if line.startswith(LINE_PREFIX):
            try:
                return json.loads(base64.b64decode(line[len(LINE_PREFIX) :]))
            except ValueError:
                return None
    return None


@dataclass
class Verification:
    present: bool
    gcode_matches: bool
    signature: str  # "valid", "invalid", "unsigned", "unchecked"
    data: dict | None

    @property
    def ok(self) -> bool:
        return self.present and self.gcode_matches and self.signature in ("valid", "unsigned", "unchecked")


def verify(gcode: str, secret: str | None = None) -> Verification:
    data = extract(gcode)
    if data is None:
        return Verification(False, False, "unchecked", None)
    matches = data.get("gcode_sha256") == gcode_digest(gcode)
    sig = data.get("sig")
    if not sig:
        state = "unsigned"
    elif not secret:
        state = "unchecked"
    else:
        expected = hmac.new(secret.encode(), _canonical(data), hashlib.sha256).hexdigest()
        state = "valid" if hmac.compare_digest(expected, sig) else "invalid"
    return Verification(True, matches, state, data)
