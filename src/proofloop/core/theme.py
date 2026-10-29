"""ProofLoop's one stylesheet (``theme.css``), put inline into every page and report.

Static pages carry a ``/*__THEME__*/`` placeholder in their first ``<style>``; Jinja templates write
``{{ theme_css }}`` there. Reports are single files you can keep or send, so nothing is linked.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from markupsafe import Markup

PLACEHOLDER = "/*__THEME__*/"
ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
        '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/></svg>')
# The same mark as a browser-tab icon
FAVICON = ("data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22 fill=%22none%22 "
           "stroke=%22%231d6fe0%22 stroke-width=%222.5%22%3E%3Cpath d=%22M21 12a9 9 0 1 1-3-6.7%22/%3E%3Cpath "
           "d=%22M21 4v5h-5%22/%3E%3Cpath d=%22m8.5 12.5 2.5 2.5 4.5-5%22/%3E%3C/svg%3E")


@cache
def css() -> str:
    return (Path(__file__).parent / "theme.css").read_text(encoding="utf-8")


def inline(html: str) -> str:
    """A static page with the stylesheet and the icon filled in."""
    return html.replace(PLACEHOLDER, css()).replace("__PROOFLOOP_ICON__", ICON).replace("__PROOFLOOP_FAVICON__", FAVICON)


def environment(package: str, folder: str, **options):
    """A Jinja environment for ``package``'s ``folder`` of templates, with the theme available to them."""
    from jinja2 import Environment, PackageLoader, select_autoescape

    env = Environment(loader=PackageLoader(package, folder), autoescape=select_autoescape(["j2", "html"]), **options)
    env.globals.update(theme_css=Markup(css()), icon=Markup(ICON), favicon=FAVICON)
    return env
