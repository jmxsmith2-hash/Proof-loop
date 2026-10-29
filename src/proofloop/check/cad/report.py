"""HTML reports for one design version and for one sliced G-code file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from proofloop.core import theme


def write_design_report(result: dict[str, Any], out_dir: Path) -> Path:
    return write_report("design.html.j2", result, out_dir)


def write_report(template: str, result: dict[str, Any], out_dir: Path) -> Path:
    env = theme.environment("proofloop.check", "templates", trim_blocks=True, lstrip_blocks=True)
    html = env.get_template(template).render(r=result)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path
