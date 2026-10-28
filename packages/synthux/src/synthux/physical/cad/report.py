"""HTML report for one design version."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape


def write_design_report(result: dict[str, Any], out_dir: Path) -> Path:
    env = Environment(loader=PackageLoader("synthux", "report/templates"), autoescape=select_autoescape(["j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    html = env.get_template("design.html.j2").render(r=result)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path
