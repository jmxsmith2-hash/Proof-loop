"""Small inline-SVG charts for the static report (no JavaScript libraries, works offline).

Colours are CSS variables defined in the report template, so light and dark
mode each use their own validated steps. Every mark has a <title> tooltip and
every chart has a table view next to it.
"""

from __future__ import annotations

from html import escape

SERIES = [f"var(--series-{i})" for i in range(1, 9)]


def bar_chart(rows: list[tuple[str, float, str]], *, width: int = 640, max_value: float = 1.0,
              percent: bool = True, label_width: int = 230) -> str:
    """Horizontal bars, one series (slot 1). rows = (label, value, tooltip)."""
    bar_h, gap = 22, 10
    height = len(rows) * (bar_h + gap) + 8
    plot_w = width - label_width - 70
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="bar chart" width="100%" preserveAspectRatio="xMinYMin meet">']
    for i, (label, value, tip) in enumerate(rows):
        y = i * (bar_h + gap) + 4
        w = max(0.0, min(1.0, value / max_value if max_value else 0)) * plot_w
        shown = f"{value:.0%}" if percent else f"{value:g}"
        parts.append(f'<g class="mark"><title>{escape(tip)}</title>'
                     f'<rect x="{label_width}" y="{y - 2}" width="{plot_w + 60}" height="{bar_h + 4}" fill="transparent"/>'
                     f'<text x="{label_width - 10}" y="{y + bar_h * 0.7:.0f}" text-anchor="end" class="axis-label">'
                     f'{escape(label[:34])}</text>')
        if w > 0:
            parts.append(f'<rect x="{label_width}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" '
                         f'fill="var(--series-1)"/>')
        parts.append(f'<text x="{label_width + w + 8:.1f}" y="{y + bar_h * 0.7:.0f}" class="value-label">'
                     f'{shown}</text></g>')
    parts.append(f'<line x1="{label_width}" y1="0" x2="{label_width}" y2="{height}" class="axis"/>')
    parts.append("</svg>")
    return "".join(parts)


def line_chart(series: list[dict], *, width: int = 470, height: int = 220, y_max: float | None = None,
               title: str = "") -> str:
    """Lines over steps. series = [{"name", "values", "slot", "threshold"}]; direct labels when <= 4 series."""
    left, right, top, bottom = 44, 150 if len(series) <= 4 else 20, 14, 30
    pw, ph = width - left - right, height - top - bottom
    longest = max((len(s["values"]) for s in series), default=1)
    peak = max([max(s["values"], default=0) for s in series] + [s.get("threshold") or 0 for s in series] + [10])
    y_max = y_max or (int(peak / 20) + 1) * 20

    def x(i: int) -> float:
        return left + (pw * i / max(1, longest - 1))

    def y(v: float) -> float:
        return top + ph - ph * min(v, y_max) / y_max

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" '
             f'width="100%" preserveAspectRatio="xMinYMin meet">']
    for tick in range(0, int(y_max) + 1, max(10, int(y_max / 4 / 10) * 10 or 10)):
        parts.append(f'<line x1="{left}" y1="{y(tick):.1f}" x2="{left + pw}" y2="{y(tick):.1f}" class="grid"/>'
                     f'<text x="{left - 6}" y="{y(tick) + 4:.1f}" text-anchor="end" class="axis-label">{tick}</text>')
    parts.append(f'<text x="{left + pw / 2:.0f}" y="{height - 6}" text-anchor="middle" class="axis-label">step</text>')
    for s in series:
        color = SERIES[s["slot"] % 8]
        vals = s["values"]
        if not vals:
            continue
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
        if s.get("threshold"):
            parts.append(f'<line x1="{left}" y1="{y(s["threshold"]):.1f}" x2="{left + pw}" y2="{y(s["threshold"]):.1f}" '
                         f'stroke="{color}" stroke-width="1" stroke-dasharray="3 4" opacity="0.6">'
                         f'<title>{escape(s["name"])}: quits at {s["threshold"]:.0f}</title></line>')
        for i, v in enumerate(vals):
            parts.append(f'<g class="mark"><title>{escape(s["name"])} - step {i + 1}: frustration {v:.0f}</title>'
                         f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="9" fill="transparent"/>'
                         f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.5" fill="{color}" class="dot"/></g>')

    if len(series) <= 4:  # direct labels at line ends, nudged apart so they never overlap
        ends = sorted(((y(s["values"][-1]), x(len(s["values"]) - 1), s["name"]) for s in series if s["values"]))
        placed: list[float] = []
        for ly, lx, name in ends:
            ly = max([ly] + [p + 13 for p in placed if abs(p - ly) < 13])
            placed.append(ly)
            parts.append(f'<text x="{lx + 8:.1f}" y="{ly + 4:.1f}" class="direct-label">{escape(name[:22])}</text>')
    parts.append(f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" class="axis"/></svg>')
    return "".join(parts)


def legend(series: list[dict]) -> str:
    items = "".join(f'<span class="legend-item"><span class="swatch" style="background:{SERIES[s["slot"] % 8]}">'
                    f'</span>{escape(s["name"])}</span>' for s in series)
    return f'<div class="legend">{items}</div>'
