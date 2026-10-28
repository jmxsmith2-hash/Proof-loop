"""Rebuild ClearLoop's built-in P2S profiles from public evidence.

The values come from a FarmLoop-processed P2S file published in
github.com/Liaminamerica2/p2s-autofarm-playbook (evidence/ folder), made with
FarmLoop app 2.72 for FarmBoard firmware 2.94. ClearLoop does not ship that
file; this script reads it from a local clone so anyone can reproduce the
profile JSON byte for byte:

    git clone https://github.com/Liaminamerica2/p2s-autofarm-playbook
    python tools/build_builtin_profiles.py p2s-autofarm-playbook/evidence
"""

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clearloop import threemf  # noqa: E402
from clearloop.importer import import_profile  # noqa: E402

PLAIN = "Poop-Bucket-Logo-V2(5)_Poop Bucket.gcode.3mf"
PROCESSED = "FL_S2_Poop-Bucket-Logo-V2(5)_Poop Bucket.gcode.3mf"
EXPECTED_SHA256 = "e75259043c768a2f4a3a3804839996c645a31194f73ebb2dc0f124c368ea9f68"
REPO = "https://github.com/Liaminamerica2/p2s-autofarm-playbook"
COMMIT = "7b9a988963bf152c3839344d441e82bdcd96b924"
OUT = Path(__file__).resolve().parent.parent / "src" / "clearloop" / "profile_data"


def main(evidence: str) -> None:
    ev = Path(evidence)
    archive = (ev / PROCESSED).read_bytes()
    digest = hashlib.sha256(archive).hexdigest()
    if digest != EXPECTED_SHA256:
        raise SystemExit(f"unexpected evidence file (sha256 {digest}); refusing to build from it")
    report = import_profile(threemf.load(ev / PLAIN), threemf.load(ev / PROCESSED), "P2S Stage 2 (FarmBoard)")
    p = report.profile
    source = {
        "kind": "builtin",
        "evidence": f"{REPO}/tree/{COMMIT}/evidence",
        "farmloop_file": PROCESSED,
        "farmloop_file_sha256": EXPECTED_SHA256,
        "farmloop_app": "2.72",
        "farmboard_firmware": "2.94",
        "slicer": p.source["slicer"],
    }

    stage2 = asdict(p)
    stage2.update(
        id="p2s-stage2-farmboard",
        name="P2S · Stage 2 (FarmBoard)",
        description="FarmLoop Stage 2 kit on a P2S: FarmBoard door, bender and pusher.",
        source=source,
        notes=[
            "FarmBoard triggers, switch gestures, waits, sweep path and park position are copied verbatim "
            "from the FarmLoop app's own P2S output.",
            "The door closes on T3 even when T2 (bend) and the safety check are skipped: observed on a P2S "
            "in the playbook's first live run (GCODE-HOWTO.md section 9). ClearLoop's 'bender removed' "
            "option relies on this.",
            "Left out on purpose: the FarmLoop app's 120-minute natural-cooldown wait that it places before "
            "the print with the heaters on (FL_S2_INCIDENT.md).",
        ],
    )
    (OUT / "p2s-stage2-farmboard.json").write_text(json.dumps(stage2, indent=2) + "\n")

    stage1 = dict(stage2)
    stage1.update(
        id="p2s-sweep-only",
        name="P2S · pusher sweep only (no FarmBoard)",
        stage=1,
        description="A FarmLoop pusher on a P2S with no FarmBoard: cool down, sweep, park. Keep the door "
        "open (or removed) during prints so parts can leave.",
        start_anchor=None,
        start_block="",
        door_open="",
        bend="",
        safety="",
        door_close="",
        notes=[
            "Uses the FarmLoop app's P2S sweep path, the same path the playbook's door-and-pusher P2S build "
            "runs without a bender.",
            "FarmLoop has not published the P2S Stage 1 kit's own G-code. If you have the Stage 1 kit, "
            "import one of your FL_S1_ files to use its exact moves, including any bend step.",
        ],
    )
    (OUT / "p2s-sweep-only.json").write_text(json.dumps(stage1, indent=2) + "\n")
    print("\n".join(report.found))
    print("dropped:", *report.dropped, sep="\n  ")


if __name__ == "__main__":
    main(sys.argv[1])
