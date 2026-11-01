"""The timelapse: pictures every few layers while a print runs, one small animation when it ends."""

import io
import json

import pytest
from PIL import Image, ImageDraw

from proofloop.core.settings import CameraSettings
from proofloop.monitor import timelapse
from proofloop.monitor.store import Store
from proofloop.monitor.timelapse import Timelapse, folder_name
from proofloop.monitor.watch import PrinterWatch
from tests.monitor.test_monitor import Link, Verifier


def picture(n=0, size=(800, 450)):
    img = Image.new("RGB", size, (60 + n % 150, 90, 120))
    ImageDraw.Draw(img).rectangle((100 + n, 100, 300 + n, 300), fill=(240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class CameraLink(Link):
    def __init__(self, total=50):
        super().__init__()
        self.state.total_layers, self.state.layer_num = total, 1
        self.taken = 0
        self.broken = lambda n: False

    def snapshot(self):
        self.taken += 1
        if self.broken(self.taken):
            raise OSError("camera went away")
        return picture(self.taken)


def watching(tmp_path, on=True, every=5, keep=5, total=50, verifier=None):
    cfg = CameraSettings(timelapse=on, timelapse_every_layers=every, timelapse_keep=keep)
    link = CameraLink(total)
    tl = Timelapse(tmp_path / "timelapse", "p2s", lambda: cfg)
    w = PrinterWatch("p2s", link, verifier or Verifier([], refs=0), Store(), settle_s=0, run_async=lambda f: f(), retry_s=0,
                     timelapse=tl)
    w.on_state(link.state)
    return w, link, tl


def print_to(w, link, last, first=2):
    for layer in range(first, last + 1):
        link.state.layer_num = layer
        w.on_state(link.state)


def finish(w, link, state="FINISH"):
    link.state.gcode_state = state
    w.on_state(link.state)
    link.state.gcode_state = "IDLE"
    w.on_state(link.state)


def test_a_print_becomes_one_small_animation_and_the_pictures_are_deleted(tmp_path):
    w, link, tl = watching(tmp_path)
    print_to(w, link, 50)
    assert link.taken == 10  # layers 1, 6, 11 ... 46
    finish(w, link)
    (item,) = tl.listing()
    assert item["frames"] == 11 and item["outcome"] == "finished" and item["job"] == "FL_S2_bucket"  # and how it ended
    assert item["kind"] in ("webp", "gif") and item["size"] > 0
    path, media_type = tl.file(item["id"])
    assert media_type == f"image/{item['kind']}"
    with Image.open(path) as im:
        assert im.width == timelapse.WIDTH and im.n_frames == 11 and im.is_animated
    assert sorted(p.name for p in tl.dir.iterdir()) == [f"{item['id']}.json", f"{item['id']}.{item['kind']}"]


def test_nothing_is_taken_unless_it_is_turned_on(tmp_path):
    w, link, tl = watching(tmp_path, on=False)
    print_to(w, link, 50)
    finish(w, link)
    assert link.taken == 0 and tl.listing() == [] and not tl.dir.exists()


def test_a_camera_error_skips_that_picture_only(tmp_path):
    w, link, tl = watching(tmp_path)
    link.broken = lambda n: n % 3 == 0
    print_to(w, link, 50)
    finish(w, link)
    assert tl.listing()[0]["frames"] == 7 + 1  # 10 asked for, 3 failed; the picture at the end was fine


def test_a_dead_camera_leaves_no_timelapse_and_no_pictures(tmp_path):
    w, link, tl = watching(tmp_path)
    link.broken = lambda n: True
    print_to(w, link, 50)
    finish(w, link)
    assert tl.listing() == [] and not list(tl.dir.rglob("*.jpg"))


def test_too_few_pictures_are_not_worth_an_animation(tmp_path):
    w, link, tl = watching(tmp_path, every=20)
    print_to(w, link, 10)  # layer 1, and the end
    finish(w, link)
    assert tl.listing() == []


def test_a_long_print_takes_pictures_further_apart(tmp_path):
    w, link, tl = watching(tmp_path, every=1, total=400)
    print_to(w, link, 400)
    finish(w, link)
    assert link.taken <= timelapse.MAX_FRAMES + 1
    assert tl.listing()[0]["frames"] <= timelapse.MAX_FRAMES


def test_only_the_last_few_are_kept(tmp_path):
    w, link, tl = watching(tmp_path, keep=2)
    for _ in range(4):
        link.state.layer_num, link.state.gcode_state = 1, "RUNNING"
        print_to(w, link, 50)
        finish(w, link)
    kept = tl.listing()
    assert len(kept) == 2 and len({k["id"] for k in kept}) == 2  # even started in the same second
    assert len(list(tl.dir.glob("*"))) == 4  # each a picture and its note


def test_a_loop_file_gets_one_timelapse_for_each_loop(tmp_path):
    w, link, tl = watching(tmp_path, verifier=Verifier([True]))
    print_to(w, link, 50)
    link.state.layer_num, link.state.gcode_state = 50, "PAUSE"  # the loop's end: the plate check follows
    w.on_state(link.state)
    link.state.gcode_state = "RUNNING"
    print_to(w, link, 50, first=50)  # the pause ends; the sweep or the next loop's start still reports layer 50
    assert len(tl.listing()) == 1
    link.state.layer_num = 1  # the next loop: the layers start over
    w.on_state(link.state)
    print_to(w, link, 50)
    finish(w, link)
    assert len(tl.listing()) == 2


def test_a_failed_print_gets_its_timelapse_too(tmp_path):
    w, link, tl = watching(tmp_path)
    print_to(w, link, 30)
    finish(w, link, "FAILED")
    assert tl.listing()[0]["outcome"] == "failed"


def test_turning_it_off_part_way_drops_the_pictures(tmp_path):
    cfg = CameraSettings(timelapse=True)
    link = CameraLink()
    tl = Timelapse(tmp_path / "timelapse", "p2s", lambda: cfg)
    w = PrinterWatch("p2s", link, Verifier([], refs=0), Store(), settle_s=0, run_async=lambda f: f(), timelapse=tl)
    w.on_state(link.state)
    print_to(w, link, 20)
    cfg.timelapse = False
    print_to(w, link, 30, first=21)
    finish(w, link)
    assert tl.listing() == [] and not any(tl.dir.rglob("*.jpg"))


def test_pictures_left_by_a_stop_in_the_middle_of_a_print_are_cleaned_up(tmp_path):
    left = tmp_path / "timelapse" / "p2s" / "20260101-101010"
    left.mkdir(parents=True)
    (left / "frame-0001.jpg").write_bytes(picture())
    Timelapse(tmp_path / "timelapse", "p2s", CameraSettings)
    assert not left.exists()


def test_a_webp_that_pillow_cannot_write_becomes_a_gif(tmp_path, monkeypatch):
    real = Image.Image.save

    def save(self, fp, format=None, **kw):
        if format == "WEBP":
            raise ValueError("no WebP animation in this Pillow")
        return real(self, fp, format, **kw)

    monkeypatch.setattr(Image.Image, "save", save)
    w, link, tl = watching(tmp_path)
    print_to(w, link, 50)
    finish(w, link)
    (item,) = tl.listing()
    assert item["kind"] == "gif" and tl.file(item["id"])[1] == "image/gif"
    assert not list(tl.dir.glob("*.webp"))


def test_a_timelapse_is_found_only_by_its_own_id(tmp_path):
    w, link, tl = watching(tmp_path)
    print_to(w, link, 50)
    finish(w, link)
    assert tl.file("../x") is None and tl.file("20200101-000000") is None


def test_printer_names_become_safe_folder_names():
    left = folder_name("P2S left")
    assert left.startswith("P2S_left-") and len(left) == len("P2S_left-") + 6
    assert folder_name("p2s") == "p2s" and folder_name("p2s-1.5") == "p2s-1.5"
    for odd in ("..", "a\\b:c", "  "):
        assert "/" not in folder_name(odd) and "\\" not in folder_name(odd) and not folder_name(odd).startswith(".")


def test_the_notes_say_where_and_when(tmp_path):
    w, link, tl = watching(tmp_path)
    print_to(w, link, 50)
    finish(w, link)
    (item,) = tl.listing()
    note = json.loads((tl.dir / f"{item['id']}.json").read_text(encoding="utf-8"))
    assert note["frames"] == item["frames"] and note["ended"] >= note["started"] and note["layers"] == 46


@pytest.mark.parametrize("name", ["p2s", "P2S left"])
def test_each_printer_has_its_own_folder(tmp_path, name):
    tl = Timelapse(tmp_path, name, CameraSettings)
    assert tl.dir.parent == tmp_path and tl.dir.name == folder_name(name)
