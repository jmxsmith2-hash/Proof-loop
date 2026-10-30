"""The changelog (``CHANGELOG.md`` in the checkout): what changed in each version, and what's new to you."""

from __future__ import annotations

import re
from pathlib import Path

HEADING = re.compile(r"^## (\d+(?:\.\d+)*)(?: · (\S+))?\s*$", re.M)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def sections(text: str) -> list[dict]:
    """Every version, newest first: ``{"version", "date", "body"}`` (the body in Markdown)."""
    marks = list(HEADING.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append({"version": m.group(1), "date": m.group(2), "body": text[m.end():end].strip()})
    return out


def newer_than(text: str, version: str) -> list[dict]:
    """The versions after ``version``: what's new to someone who has it."""
    return [s for s in sections(text) if _key(s["version"]) > _key(version)]


def notes(text: str, version: str) -> str:
    """One version's notes, for its GitHub release."""
    for s in sections(text):
        if s["version"] == version:
            return s["body"]
    raise KeyError(f"no section for {version} in the changelog")


def read() -> str | None:
    """This checkout's changelog (none when ProofLoop wasn't installed from the repository)."""
    from proofloop.app.autostart import repo_root

    root = repo_root()
    path = (root / "CHANGELOG.md") if root else None
    try:
        return path.read_text(encoding="utf-8") if path else None
    except OSError:
        return None


def as_text(found: list[dict]) -> str:
    """For the terminal: headings and bullets, without Markdown's ``**``."""
    lines = []
    for s in found:
        lines.append(f"{s['version']}" + (f" ({s['date']})" if s["date"] else ""))
        lines += ["  " + line.replace("**", "") for line in s["body"].splitlines() if line.strip()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """``python -m proofloop.core.changelog VERSION [FILE]``: print a version's release notes."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    path = Path(args[1]) if len(args) > 1 else Path("CHANGELOG.md")
    print(notes(path.read_text(encoding="utf-8"), args[0].removeprefix("v")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
