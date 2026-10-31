"""What real prints on this printer and material taught."""

from __future__ import annotations

from proofloop.check.gcode.context import GcodeContext


def check_history(ctx: GcodeContext) -> None:
    """What real prints on this printer + material taught (see proofloop.check.learn)."""
    from datetime import datetime

    from proofloop.check.learn import CAUSE_TEXT, job_key

    h = ctx.history
    if not h or not h.get("prints"):
        return
    m, mat = ctx.metrics, ctx.material["name"]
    who = f"your {ctx.label}"
    peak = m.get("peak_flow_mm3_s")
    ctx.metrics["history"] = {k: h.get(k) for k in ("prints", "finished", "failed", "by_cause", "proven_flow",
                                                    "failed_flow", "eject_checked", "eject_left")}
    if peak and h.get("failed_flow") and peak >= 0.95 * h["failed_flow"]:
        ctx.add("history", 3, f"Prints have failed on {who} at this flow",
                f"This file asks for up to {peak:.0f} mm³/s. {h['flow_failures']} {mat} print(s) on {who} failed on "
                f"layers asking for about {h['failed_flow']:.0f} mm³/s.",
                f"Set the filament's max volumetric speed to about {0.85 * h['failed_flow']:.0f} mm³/s for this printer.",
                image="flow")
    elif peak and h.get("proven_flow") and peak <= h["proven_flow"]:
        for f in ctx.findings:
            if f.check == "flow":
                f.severity = 1
                f.title += f" (but {who} prints it fine)"
                f.detail += (f" Still, {h['proven_prints']} {mat} prints on {who} finished at up to "
                             f"{h['proven_flow']:.0f} mm³/s, so it handles this flow.")
    firsts = h["by_cause"].get("first_layer", 0)
    if firsts >= 2 and firsts / max(h["prints"], 1) >= 0.2:
        ctx.add("history", 2, f"{firsts} of your last {h['prints']} {mat} prints failed in the first layers",
                f"On {who}, {firsts} {mat} prints stopped in the first three layers: the first layer isn't sticking "
                "reliably on this printer.",
                "Wash the plate with dish soap and water, check the plate type matches the slicer, raise the bed "
                "5 °C, and slow the first layer.", layer=0, image="layer1")
    name = job_key(ctx.trace.source.split(" (plate")[0])
    for prev in [j for j in h.get("job") or [] if j["outcome"] == "failed"][:1]:
        when = datetime.fromtimestamp(prev["ended_at"]).strftime("%d %b %Y")
        where = f"layer {prev['layer']}" + (f" of loop {prev['loop_no']}" if prev.get("loop_no") else "")
        why = CAUSE_TEXT.get(prev["cause"] or "unknown", "")
        hms = f" The printer reported HMS {', '.join(prev['hms'])}." if prev.get("hms") else ""
        ctx.add("history", 3, f"This file failed before, at {where}",
                f"{name} failed on {who} on {when}, at {where}, {why}.{hms}",
                "Look at that layer in the preview, and fix the cause before printing it again.")
    left = [j for j in h.get("job") or [] if j.get("eject") == "parts_left"]
    if left:
        ctx.add("history", 2, f"Parts were left on the plate {len(left)} time(s)",
                f"After {len(left)} print(s) of {name}, the plate check found parts still on the plate: the sweep "
                "didn't push everything off.",
                "Let the bed cool further before the sweep (lower release temperature), or push that part lower "
                "in Make a loop file.")
