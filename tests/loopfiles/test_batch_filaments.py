"""Batches whose plates use different filaments: fine while each slot number means one filament."""

import io
import re
import zipfile

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.processor import ProcessError, compatibility, merged_slots, process_batch
from proofloop.loopfiles.gcode.routines import LoopSettings
from proofloop.loopfiles.profiles import ProfileStore
from proofloop.monitor.service import App
from tests.monitor.test_send import FakeLink, state
from tests.plates import BUCKET, SMALL, plain

P = ProfileStore().get("p2s-stage2-farmboard")
WHITE, RED, BLACK = ("PLA", "#F4F4EFFF"), ("PLA", "#C8102DFF"), ("PLA", "#000000FF")


def plate(boxes, slots: dict[str, tuple[str, str]], name: str) -> threemf.SlicedFile:
    """A plate that uses these filament slots, as Bambu Studio lists them in slice_info."""
    raw = plain(boxes)
    zin = zipfile.ZipFile(io.BytesIO(raw))
    xml = zin.read("Metadata/slice_info.config").decode()
    xml = re.sub(r"<filament [^>]*/>", "".join(
        f'<filament id="{sid}" tray_info_idx="GFA00" type="{t}" color="{c}" used_m="1" used_g="{int(sid) * 3}"/>'
        for sid, (t, c) in slots.items()), xml)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in zin.namelist():
            z.writestr(n, xml if n == "Metadata/slice_info.config" else zin.read(n))
    return threemf.loads(buf.getvalue(), name=name)


def test_plates_of_one_project_mix_in_a_batch_and_the_file_lists_every_filament():
    a = plate(BUCKET, {"1": WHITE, "2": RED}, "a.gcode.3mf")  # white body, red details
    b = plate(SMALL, {"2": RED}, "b.gcode.3mf")  # red only: the same slot 2 as plate A
    c = plate(SMALL, {"3": BLACK}, "c.gcode.3mf")  # a filament plate A doesn't use, in its own slot
    assert compatibility(a, b) == [] and compatibility(a, c, [a, b]) == []
    assert merged_slots([a, b, c]) == [("1", *WHITE), ("2", *RED), ("3", *BLACK)]
    made = threemf.loads(process_batch([(a, 2), (b, 1), (c, 1)], P, LoopSettings()).data)
    assert made.info.filament_slots == [("1", "PLA", "#F4F4EFFF"), ("2", "PLA", "#C8102DFF"), ("3", "PLA", "#000000FF")]
    assert made.info.slot_grams == {"1": 3.0, "2": 6.0, "3": 9.0}  # each as its own plate lists it


def test_a_slot_meaning_two_filaments_is_refused_with_how_to_fix_it():
    a = plate(BUCKET, {"1": WHITE, "2": RED}, "a.gcode.3mf")
    b = plate(SMALL, {"2": BLACK}, "b.gcode.3mf")
    (why,) = compatibility(a, b)
    assert "b.gcode.3mf uses slot 2 for PLA #000000, but a.gcode.3mf uses slot 2 for PLA #C8102D" in why
    assert "give each filament its own slot" in why
    try:
        process_batch([(a, 1), (b, 1)], P, LoopSettings())
    except ProcessError as e:
        assert "slot 2" in str(e)
    else:
        raise AssertionError("a conflicting batch was made")
    c = plate(SMALL, {"3": BLACK}, "c.gcode.3mf")
    d = plate(SMALL, {"3": WHITE}, "d.gcode.3mf")  # clashes with c, not with the first plate
    assert compatibility(a, d, [a, c]) and not compatibility(a, d)


def test_a_mixed_batch_is_sent_with_every_filament_mapped_to_its_tray(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    app.add_printer(PrinterConfig(name="p2s", host="127.0.0.1", serial="01", access_code="12345678", model_id="N7"))
    try:
        app.settings.send_to_printer = True
        ams = {"ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "F4F4EFFF"}, {"id": "1", "tray_type": "PLA", "tray_color": "C12E1FFF"},
            {"id": "2", "tray_type": "PLA", "tray_color": "000000FF"}]}]}}
        app.watches["p2s"].link.state = state("FINISH", ams=ams)
        c = TestClient(create_app(app))
        tokens = [app.uploads.put(threemf.only_plate(_raw(sf), sf.plate), sf.name) for sf in (
            plate(BUCKET, {"1": WHITE, "2": RED}, "a.gcode.3mf"), plate(SMALL, {"3": BLACK}, "c.gcode.3mf"))]
        body = {"items": [{"token": t, "count": 1} for t in tokens], "settings": {}}
        preview = c.post("/api/batch/preview", json=body).json()
        assert preview["ok"] and [f["slot"] for f in preview["filaments"]] == ["1", "2", "3"]
        (t,) = c.post("/api/batch/printers", json=body).json()["printers"]
        assert t["ready"] and [s["tray_label"] for s in t["slots"]] == ["A1", "A2", "A3"]
        assert c.post("/api/batch/send", json={**body, "printer": "p2s"}).status_code == 400  # not confirmed
        r = c.post("/api/batch/send", json={**body, "printer": "p2s", "confirm": "p2s"})
        assert r.status_code == 200, r.text
        name, _, cmd = app.watches["p2s"].link.sent[0]
        assert name.startswith("FL_S2_batch-2x-") and cmd["print"]["ams_mapping"] == [0, 1, 2]
    finally:
        app.shutdown()


def test_asking_before_each_send_can_be_turned_off(tmp_path):
    app = App(tmp_path, connect=False)
    try:
        c = TestClient(create_app(app))
        assert c.get("/api/state").json()["send_confirm"] is True
        assert c.put("/api/send-to-printer", json={"confirm": False}).json() == {"send_to_printer": False,
                                                                                "send_confirm": False}
        assert c.get("/api/state").json()["send_confirm"] is False and app.settings.send_to_printer is False
        assert c.put("/api/send-to-printer", json={"on": True}).json()["send_confirm"] is False  # left as it was
    finally:
        app.shutdown()


def _raw(sf: threemf.SlicedFile) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in sf._order:
            z.writestr(n, sf._entries[n])
    return buf.getvalue()
